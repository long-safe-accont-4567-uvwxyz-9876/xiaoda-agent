"""后台委托（delegate_task background=true）单元测试。

覆盖 2026-08-25 review 提出的"子代理后台执行 + 结果主动推送"模型：
注册表语义、按通道投递路由、执行体成功/失败路径、工具 schema 契约。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import async_delegation as ad
from core.async_delegation import BackgroundDelegation


def _job(channel: str = "ws", task_id: str = "t1", **kw) -> BackgroundDelegation:
    return BackgroundDelegation(
        task_id=task_id, agent="xiaoli", display_name="小莉",
        task_text="测试任务", channel=channel,
        session_id="web_test_1" if channel in ("ws", "web") else "",
        user_openid="openid-abc" if channel == "qq" else "", **kw)


@pytest.fixture(autouse=True)
def _clean_registry():
    ad._JOBS.clear()
    yield
    ad._JOBS.clear()


# ── 注册表语义 ───────────────────────────────────────────────


def test_register_markdone_snapshot():
    job = _job()
    assert ad.register(job) == "t1"
    assert ad.get("t1") is job
    assert ad.snapshot()[0]["status"] == "running"

    ad.mark_done("t1", ok=True, preview="结果文本")
    snap = ad.snapshot()[0]
    assert snap["status"] == "completed"
    assert snap["result_preview"] == "结果文本"
    assert snap["finished_at"] is not None


def test_mark_done_unknown_id_is_noop():
    ad.mark_done("ghost", ok=True)
    assert ad.get("ghost") is None


# ── 投递路由矩阵 ─────────────────────────────────────────────


async def test_deliver_ws_routes_to_session(monkeypatch):
    sent = []

    class FakeManager:
        async def send_to_session(self, session_id, event):
            sent.append((session_id, event))

    import web.ws_hub as hub
    monkeypatch.setattr(hub, "manager", FakeManager())

    job = _job(channel="ws")
    ok = await ad.deliver(job, "检索汇总结果")

    assert ok and job.status == "delivered"
    sid, event = sent[0]
    assert sid == "web_test_1"
    assert event["type"] == "delegate_result"
    assert event["ok"] is True
    assert "检索汇总结果" in event["reply"]
    assert "已完成" in event["header"]


async def test_deliver_qq_passes_openid(monkeypatch):
    captured = {}

    async def fake_send(text, openid=None):
        captured["text"], captured["openid"] = text, openid
        return True

    import qq_bot_adapter
    monkeypatch.setattr(qq_bot_adapter, "send_proactive_message", fake_send)

    job = _job(channel="qq")
    assert await ad.deliver(job, "QQ 结果") is True
    assert captured["openid"] == "openid-abc"
    assert "小莉" in captured["text"] and "QQ 结果" in captured["text"]


async def test_deliver_failure_marks_deliver_failed(monkeypatch):
    async def boom(text, openid=None):
        raise RuntimeError("bot 未连接")

    import qq_bot_adapter
    monkeypatch.setattr(qq_bot_adapter, "send_proactive_message", boom)

    job = _job(channel="qq")
    assert await ad.deliver(job, "内容") is False
    assert job.status == "deliver_failed"


async def test_deliver_unknown_channel_logs_and_succeeds():
    job = _job(channel="")
    assert await ad.deliver(job, "任意") is True
    assert job.status == "delivered"


# ── 执行体：成功 / 失败 / 空回复 / 未知代理 ─────────────────────


class _FakeManager:
    """run_background_delegation 的最小宿主（绕开真实构造依赖）。"""

    def __init__(self, dispatch_impl, known=("xiaoli",)):
        self.dispatcher = types.SimpleNamespace(
            get_agent=lambda name: object() if name in known else None)
        self.context = types.SimpleNamespace(shared_blackboard=None)
        self.blackboard_writes = None

        async def dispatch(name, task, context=None, status_callback=None,
                           address_term=""):
            return await dispatch_impl(task)

        self._dispatch_impl = dispatch_impl

    async def _dispatch_and_record(self, name, task, context, _ctx, agent,
                                   timeout_s=None):
        # 契约钉：后台模式必须传 timeout_s=None，不允许外层取消丢工作
        assert timeout_s is None
        return await self._dispatch_impl(task), 0.5

    def _build_sub_agent_context(self, task_hint=""):
        return {"hint": task_hint}

    async def _verify_result(self, name, task, result, mode, verifier):
        return result

    def _bb_task_key(self, name, task, user_id=""):
        return f"{name}:{task}"

    async def _write_blackboard_cache(self, bb, key, value, agent):
        self.blackboard_writes = (key, value)


async def _ret(value):
    return value


def _spy_deliver(bucket):
    async def deliver(job, content, failed=False):
        bucket.append((failed, content))
        # 逐字复刻真实 deliver 的终态迁移（ok=False 且 failed=True → "failed"）
        job.status = ("delivered" if not failed
                      else "failed")
        return True
    return deliver


async def test_run_background_success_delivers_result(monkeypatch):
    events = []
    monkeypatch.setattr(ad, "deliver", _spy_deliver(events))

    mgr = _FakeManager(lambda task: _ret("小莉的完整分析结论"))
    job = _job(channel="ws")
    ad.register(job)

    from agent_core.sub_agent_manager import SubAgentManagerMixin
    await SubAgentManagerMixin.run_background_delegation(mgr, job)

    assert events == [(False, "小莉的完整分析结论")]
    assert job.status == "delivered"
    assert job.result_preview.startswith("小莉的完整")
    assert mgr.blackboard_writes is not None  # 黑板缓存已写（供后续委托复用）


async def test_run_background_dispatch_error_notifies_user(monkeypatch):
    events = []
    monkeypatch.setattr(ad, "deliver", _spy_deliver(events))

    async def failing(task):
        raise TimeoutError("provider 卡死")

    mgr = _FakeManager(failing)
    job = _job(channel="qq")
    ad.register(job)

    from agent_core.sub_agent_manager import SubAgentManagerMixin
    await SubAgentManagerMixin.run_background_delegation(mgr, job)

    assert any(failed and "执行出错" in c for failed, c in events)
    assert job.status == "failed"


async def test_run_background_empty_reply_notifies_user(monkeypatch):
    events = []
    monkeypatch.setattr(ad, "deliver", _spy_deliver(events))

    mgr = _FakeManager(lambda task: _ret(""))
    job = _job(channel="cli")
    ad.register(job)

    from agent_core.sub_agent_manager import SubAgentManagerMixin
    await SubAgentManagerMixin.run_background_delegation(mgr, job)

    assert any(failed and "没有返回有效结果" in c for failed, c in events)


async def test_run_background_unknown_agent_notifies_user(monkeypatch):
    events = []
    monkeypatch.setattr(ad, "deliver", _spy_deliver(events))

    mgr = _FakeManager(lambda task: _ret("x"), known=())
    job = _job(channel="cli")
    job.agent = "不存在的代理"
    ad.register(job)

    from agent_core.sub_agent_manager import SubAgentManagerMixin
    await SubAgentManagerMixin.run_background_delegation(mgr, job)
    assert any("找不到名为" in c for failed, c in events if failed)


# ── 工具 schema 契约 ─────────────────────────────────────────


def test_delegate_task_schema_has_background_param():
    from tool_engine.tool_registry import get_tool

    tool = get_tool("delegate_task")
    if tool is None:
        pytest.skip("delegate_task 未注册（bootstrap 未初始化的轻量环境）")
    props = tool["schema"]["properties"]
    assert props["background"]["type"] == "boolean"
    assert "主动推送" in props["background"]["description"]


def test_bootstrap_interim_receipt_contract_in_source():
    """受理回执契约静态钉：background 分支必须"立即回执 + 不等结果"。"""
    src = (Path(__file__).parent.parent / "core" / "bootstrap.py").read_text(
        encoding="utf-8")
    assert "_spawn(_runner())" in src
    assert "不要等待结果" in src
    assert "ad.register(job)" in src


async def test_deliver_web_channel_normalized_to_ws(monkeypatch):
    """RequestContext.source='web' 必须归一到 ws 投递出口（14:39 事故修复钉）。"""
    sent = []

    class FakeManager:
        async def send_to_session(self, session_id, event):
            sent.append((session_id, event))

    import web.ws_hub as hub
    monkeypatch.setattr(hub, "manager", FakeManager())

    job = _job(channel="web")
    ok = await ad.deliver(job, "结果")

    assert ok and job.status == "delivered"
    assert sent and sent[0][0] == "web_test_1"
