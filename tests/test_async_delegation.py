"""后台委托 v2 单元测试：无回执/无专用帧形态 + 主代理口吻转述 + 控制三件套。

用户定稿模型（2026-08-25）：
- delegate_task(background=true) 后主代理正常流式输出，任务静默后台执行；
- 完成后结果交回**主代理本人**，由其用自己口吻转述，经普通消息通道发出
  （ws 即标准 final 帧，无新帧类型）；
- 主代理可随时 status 查进度 / abort 终止 / interject 向运行中任务插话。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import async_delegation as ad
from core.async_delegation import BackgroundDelegation


def _job(channel: str = "web", task_id: str = "t1", **kw) -> BackgroundDelegation:
    base = dict(
        task_id=task_id, agent="xiaoli", display_name="小莉",
        task_text="对比 Rust 与 Python 的优劣", channel=channel,
        session_id="web_test_1" if channel in ("ws", "web") else "",
        user_openid="openid-abc" if channel == "qq" else "",
        address_term="爸爸",
    )
    base.update(kw)
    return BackgroundDelegation(**base)


def _request_context(*, user_id: str, session_id: str, channel: str = "web"):
    from agent_core._shared import RequestContext

    return RequestContext(user_id=user_id, session_id=session_id, channel=channel)


def _capture_registered_tools(monkeypatch, core):
    from core.bootstrap import AgentCoreBootstrapper
    from tool_engine import tool_registry

    tools = {}

    def capture(**definition):
        tools[definition["name"]] = definition["func"]

    monkeypatch.setattr(tool_registry, "register_tool_direct", capture)
    AgentCoreBootstrapper(core)._register_delegate_tool()
    return tools


class _FakeCore:
    """compose_and_deliver 所需最小 core 面：router.route 可编程。"""

    def __init__(self, route_impl=None):
        self.router = types.SimpleNamespace(route=self._route)
        self._impl = route_impl or (lambda *a, **k: _ret("转述后的自然话语"))

    async def _route(self, task_type, messages, **kw):
        self.last_messages = messages
        return await self._impl(task_type, messages, kw)

    async def __call__route(self):  # pragma: no cover
        pass


async def _ret(value):
    return value


def _spy_send(monkeypatch, sink):
    """把 ws/qq 出口替换为记录器。"""

    class FakeManager:
        async def send_to_session(self, session_id, event):
            sink.append(("ws", session_id, event))

    import web.ws_hub as hub
    monkeypatch.setattr(hub, "manager", FakeManager())

    import qq_bot_adapter
    monkeypatch.setattr(qq_bot_adapter, "send_proactive_message", fake_qq(sink))


def fake_qq(sink):
    async def send_proactive_message(text, openid=None):
        sink.append(("qq", openid, text))
        return True
    return send_proactive_message


@pytest.fixture(autouse=True)
def _clean_registry():
    ad._JOBS.clear()
    yield
    ad._JOBS.clear()


# ── 注册表语义 ───────────────────────────────────────────────


def test_register_status_snapshot():
    job = _job(task_id="t1")
    ad.register(job)

    snap = ad.snapshot()[0]
    assert snap["status"] == "running"
    assert snap["pending_interjections"] == 0
    assert "elapsed_s" in snap and "asyncio_task" not in snap

    ad.mark_done("t1", ok=True, preview="结果")
    job.interjections.append("补充一句")  # 未消费插话计数
    snap = ad.snapshot()[0]
    assert snap["status"] == "completed" and snap["result_preview"] == "结果"
    assert snap["pending_interjections"] == 1


def test_mark_cancelled():
    job = _job()
    ad.register(job)
    ad.mark_cancelled("t1")
    assert ad.snapshot()[0]["status"] == "cancelled"


def test_find_running_filters():
    j1 = _job(channel="ws", task_id="bg-xiaoli-a")
    j2 = _job(channel="ws", task_id="bg-xiaoke-b", agent="xiaoke")
    for j in (j1, j2):
        ad.register(j)

    assert ad.find_running() in (j1, j2)
    assert ad.find_running(agent="xiaoli") is j1
    assert ad.find_running(agent="xiaoli", task_id_prefix="bg-xiaoke") is None
    assert ad.find_running(agent="xiaolang") is None


async def test_concurrent_request_contexts_isolate_snapshot_and_find_running():
    from agent_core._shared import _current_request_ctx

    alice = _job(
        task_id="bg-xiaoli-alice",
        user_id="alice",
        session_id="session-a",
    )
    bob = _job(
        task_id="bg-xiaoli-bob",
        user_id="bob",
        session_id="session-b",
    )
    ad.register(alice)
    ad.register(bob)

    ready = 0
    both_ready = asyncio.Event()

    async def inspect(ctx, own_job, foreign_job):
        nonlocal ready
        token = _current_request_ctx.set(ctx)
        try:
            ready += 1
            if ready == 2:
                both_ready.set()
            await both_ready.wait()
            await asyncio.sleep(0)
            visible = ad.snapshot()
            return (
                [item["task_id"] for item in visible],
                ad.find_running(task_id_prefix=own_job.task_id),
                ad.find_running(task_id_prefix=foreign_job.task_id),
            )
        finally:
            _current_request_ctx.reset(token)

    alice_view, bob_view = await asyncio.gather(
        inspect(
            _request_context(user_id="alice", session_id="session-a"),
            alice,
            bob,
        ),
        inspect(
            _request_context(user_id="bob", session_id="session-b"),
            bob,
            alice,
        ),
    )

    assert alice_view == ([alice.task_id], alice, None)
    assert bob_view == ([bob.task_id], bob, None)


def test_registry_ownership_requires_user_session_and_channel():
    from agent_core._shared import _current_request_ctx

    owner = _request_context(user_id="alice", session_id="session-a", channel="web")
    jobs = [
        _job(task_id="owned", user_id="alice", session_id="session-a", channel="web"),
        _job(task_id="wrong-user", user_id="bob", session_id="session-a", channel="web"),
        _job(task_id="wrong-session", user_id="alice", session_id="session-b", channel="web"),
        _job(task_id="wrong-channel", user_id="alice", session_id="session-a", channel="qq"),
    ]
    for job in jobs:
        ad.register(job)

    token = _current_request_ctx.set(owner)
    try:
        assert [item["task_id"] for item in ad.snapshot()] == ["owned"]
        assert ad.find_running(task_id_prefix="owned") is jobs[0]
        for foreign in jobs[1:]:
            assert ad.find_running(task_id_prefix=foreign.task_id) is None
    finally:
        _current_request_ctx.reset(token)


async def test_control_tool_rejects_foreign_full_task_id(monkeypatch):
    from agent_core._shared import _current_request_ctx

    core = types.SimpleNamespace(
        _agent_route_configs={},
        sticker_manager=types.SimpleNamespace(available=False),
    )
    control = _capture_registered_tools(monkeypatch, core)["sub_agent_control"]

    class TaskHandle:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    own = _job(
        task_id="bg-xiaoli-own-full-id",
        user_id="alice",
        session_id="session-a",
    )
    foreign = _job(
        task_id="bg-xiaoli-foreign-full-id",
        user_id="bob",
        session_id="session-b",
    )
    own.asyncio_task = TaskHandle()
    foreign.asyncio_task = TaskHandle()
    ad.register(own)
    ad.register(foreign)

    token = _current_request_ctx.set(
        _request_context(user_id="alice", session_id="session-a")
    )
    try:
        status = await control("status")
        interject = await control(
            "interject", target=foreign.task_id, message="泄露给另一个会话"
        )
        abort_foreign = await control("abort", target=foreign.task_id)
        own_interject = await control(
            "interject", target=own.task_id, message="只给自己的补充"
        )
        abort_own = await control("abort", target=own.task_id)
    finally:
        _current_request_ctx.reset(token)

    assert own.task_id in status.data
    assert foreign.task_id not in status.data
    assert not interject.success and foreign.interjections == []
    assert not abort_foreign.success and not foreign.asyncio_task.cancelled
    assert own_interject.success and own.interjections == ["只给自己的补充"]
    assert abort_own.success and own.asyncio_task.cancelled


# ── 转述投递：普通消息语义（无专用帧） ───────────────────────────


async def test_compose_uses_main_llm_and_sends_final_frame(monkeypatch):
    sink = []
    _spy_send(monkeypatch, sink)
    core = _FakeCore(lambda *a, **k: _ret("爸爸，Rust 快但难写，Python 慢但省心～"))

    job = _job(channel="web")
    ok = await ad.compose_and_deliver(core, job, "完整分析内容……")

    assert ok and job.status == "delivered"
    kind, sid, frame = sink[0]
    assert (kind, sid) == ("ws", "web_test_1")
    # 标准 final 帧：前端按普通回复渲染，无任何新帧类型
    assert frame["type"] == "final"
    assert "Rust 快但难写" in frame["reply"]
    # 转述提示包含原任务与结果
    msgs = core.last_messages
    assert any("对比 Rust 与 Python" in m["content"] for m in msgs)


async def test_compose_llm_failure_falls_back_to_template(monkeypatch):
    sink = []
    _spy_send(monkeypatch, sink)

    async def broken(*a, **k):
        raise TimeoutError("30s 墙")

    core = _FakeCore(broken)
    job = _job(channel="web")
    ok = await ad.compose_and_deliver(core, job, "可靠到达的结果")

    assert ok and job.status == "delivered"
    _, _, frame = sink[0]
    # 模板降级：地址称谓 + 结果原文必达
    assert "爸爸" in frame["reply"] and "可靠到达的结果" in frame["reply"]


async def test_failed_result_uses_failed_wording(monkeypatch):
    sink = []
    _spy_send(monkeypatch, sink)
    core = _FakeCore(lambda *a, **k: _ret("x"))

    job = _job(channel="web")
    await ad.compose_and_deliver(core, job, "执行出错了：xxx", failed=True)

    _, _, frame = sink[0]
    assert "没能完成" in frame["reply"]


async def test_unconsumed_interjections_entered_into_narration(monkeypatch):
    sink = []
    _spy_send(monkeypatch, sink)

    captured = {}

    async def capture_route(task_type, messages, *args, **kw):
        captured["user"] = messages[-1]["content"]
        return "好的，会连同你的补充一起说明"

    core = _FakeCore(capture_route)
    job = _job(channel="web")
    job.interjections.append("重点讲内存安全")
    await ad.compose_and_deliver(core, job, "结果")

    assert "内存安全" in captured["user"] and "务必一并回应" in captured["user"]


async def test_deliver_text_qq_direct(monkeypatch):
    sink = []
    import qq_bot_adapter
    monkeypatch.setattr(qq_bot_adapter, "send_proactive_message", fake_qq(sink))

    job = _job(channel="qq")
    ok = await ad.deliver_text(job, "已按你说的停下来了。")

    assert ok and job.status == "delivered"
    assert sink[0][0] == "qq"


# ── 执行体：成功 / 失败 / 取消 / 未知代理 ────────────────────────


class _FakeManager:
    def __init__(self, dispatch_impl, known=("xiaoli",)):
        self.dispatcher = types.SimpleNamespace(
            get_agent=lambda name: object() if name in known else None)
        self.context = types.SimpleNamespace(shared_blackboard=None,
                                             current_address_term="爸爸")
        self.seen_kwargs = {}
        self.blackboard_writes = None

        async def dispatch(name, task, context="", status_callback=None,
                           address_term="", extra_system_prompt="",
                           interjections=None):
            self.seen_kwargs = {"interjections": interjections,
                                "status_cb": status_callback}
            return await dispatch_impl(task)

        self._dispatch_impl = dispatch_impl

    async def _dispatch_and_record(self, name, task, context, _ctx, agent,
                                   timeout_s=None, interjections=None):
        assert timeout_s is None, "后台模式不允许外层超时取消"
        self.seen_kwargs = {"interjections": interjections}
        if self._dispatch_impl is not None:
            return await self._dispatch_impl(task), 0.4
        return "默认结果", 0.4

    def _build_sub_agent_context(self, task_hint=""):
        return {"hint": task_hint}

    async def _verify_result(self, name, task, result, mode, verifier):
        return result

    def _bb_task_key(self, name, task, user_id=""):
        return f"{name}:{task}"

    async def _write_blackboard_cache(self, bb, key, value, agent):
        self.blackboard_writes = (key, value)


def _spy_compose(bucket):
    async def compose(core, job, result, *, failed=False):
        bucket.append((failed, result))
        job.status = "failed" if failed else "delivered"
        return True
    return compose


async def test_runner_success_composes_via_main_core(monkeypatch):
    events = []
    monkeypatch.setattr(ad, "compose_and_deliver",
                        _spy_compose(events))

    mgr = _FakeManager(lambda task: _ret("小莉的最终结论"))
    job = _job(channel="web")
    ad.register(job)
    job.asyncio_task = asyncio.current_task()

    from agent_core.sub_agent_manager import SubAgentManagerMixin as M
    await M.run_background_delegation(mgr, job)

    assert events == [(False, "小莉的最终结论")]
    assert mgr.blackboard_writes is not None
    assert job.status == "delivered"


async def _spy_call(events, failed, result):
    events.append((failed, result))
    return True


async def test_runner_cancelled_sends_stop_notice(monkeypatch):
    events = []

    async def fake_deliver_text(job, text, *, failed=False):
        events.append(text)
        job.status = "cancelled"
        return True

    async def cancelled_dispatch(task):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ad, "deliver_text", fake_deliver_text)
    mgr = _FakeManager(cancelled_dispatch)
    job = _job(channel="ws")
    ad.register(job)

    from agent_core.sub_agent_manager import SubAgentManagerMixin as M
    await M.run_background_delegation(mgr, job)

    assert any("停下来" in t for t in events)
    assert job.status == "cancelled"


async def test_runner_error_composes_failure(monkeypatch):
    bucket = []
    monkeypatch.setattr(ad, "compose_and_deliver", _spy_compose(bucket))

    async def failing(task):
        raise RuntimeError("provider 500")

    mgr = _FakeManager(failing)
    job = _job(channel="web")
    ad.register(job)

    from agent_core.sub_agent_manager import SubAgentManagerMixin as M
    await M.run_background_delegation(mgr, job)

    assert bucket == [(True, "执行出错了：provider 500")]


async def test_delegate_background_saves_handle_aborts_and_reclaims(monkeypatch):
    from agent_core._shared import _current_request_ctx
    from core.background_tasks import BackgroundTaskManager

    started = asyncio.Event()
    stopped = asyncio.Event()

    async def run_background_delegation(job):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            ad.mark_cancelled(job.task_id)
            stopped.set()

    manager = BackgroundTaskManager(MagicMock(), MagicMock())
    core = types.SimpleNamespace(
        _agent_route_configs={
            "xiaoli": {"display_name": "小莉", "route_description": "测试"}
        },
        _bg_task_manager=manager,
        delegate_to_agent=None,
        run_background_delegation=run_background_delegation,
        sticker_manager=types.SimpleNamespace(available=False),
    )
    tools = _capture_registered_tools(monkeypatch, core)
    delegate = tools["delegate_task"]
    control = tools["sub_agent_control"]
    ctx = _request_context(user_id="alice", session_id="session-a")

    token = _current_request_ctx.set(ctx)
    try:
        result = await delegate("xiaoli", "保持运行", background=True)
        await started.wait()
        job = ad.snapshot()[0]
        registered = ad.get(job["task_id"])

        assert result.success
        assert registered is not None
        assert registered.asyncio_task in manager.get_owned_tasks()

        aborted = await control("abort", target=registered.task_id)
        assert aborted.success
        await stopped.wait()
        await asyncio.sleep(0)
    finally:
        _current_request_ctx.reset(token)
        await manager.cancel_background_tasks()

    assert registered.status == "cancelled"
    assert registered.asyncio_task.done()
    assert registered.asyncio_task not in manager.get_owned_tasks()


async def test_delegate_runner_crash_uses_existing_fallback_delivery(monkeypatch):
    from agent_core._shared import _current_request_ctx
    from core.background_tasks import BackgroundTaskManager

    delivered = asyncio.Event()
    calls = []

    async def crash(job):
        raise RuntimeError("runner exploded")

    async def fake_deliver_text(job, text, *, failed=False):
        calls.append((job, text, failed))
        delivered.set()
        return True

    monkeypatch.setattr(ad, "deliver_text", fake_deliver_text)
    manager = BackgroundTaskManager(MagicMock(), MagicMock())
    core = types.SimpleNamespace(
        _agent_route_configs={
            "xiaoli": {"display_name": "小莉", "route_description": "测试"}
        },
        _bg_task_manager=manager,
        delegate_to_agent=None,
        run_background_delegation=crash,
        sticker_manager=types.SimpleNamespace(available=False),
    )
    delegate = _capture_registered_tools(monkeypatch, core)["delegate_task"]
    token = _current_request_ctx.set(
        _request_context(user_id="alice", session_id="session-a")
    )
    try:
        result = await delegate("xiaoli", "触发崩溃", background=True)
        await asyncio.wait_for(delivered.wait(), timeout=1)
        await asyncio.sleep(0)
    finally:
        _current_request_ctx.reset(token)
        await manager.cancel_background_tasks()

    job, text, failed = calls[0]
    assert result.success
    assert job.status == "failed"
    assert "runner exploded" in text
    assert failed is True
    assert job.asyncio_task.done()
    assert job.asyncio_task not in manager.get_owned_tasks()


async def test_manager_shutdown_cancels_and_reclaims_delegation(monkeypatch):
    from agent_core._shared import _current_request_ctx
    from core.background_tasks import BackgroundTaskManager

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run_background_delegation(job):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            ad.mark_cancelled(job.task_id)
            cancelled.set()

    manager = BackgroundTaskManager(MagicMock(), MagicMock())
    core = types.SimpleNamespace(
        _agent_route_configs={
            "xiaoli": {"display_name": "小莉", "route_description": "测试"}
        },
        _bg_task_manager=manager,
        delegate_to_agent=None,
        run_background_delegation=run_background_delegation,
        sticker_manager=types.SimpleNamespace(available=False),
    )
    delegate = _capture_registered_tools(monkeypatch, core)["delegate_task"]
    token = _current_request_ctx.set(
        _request_context(user_id="alice", session_id="session-a")
    )
    try:
        await delegate("xiaoli", "等待停机", background=True)
        await started.wait()
        job = ad.get(ad.snapshot()[0]["task_id"])
        await manager.cancel_background_tasks()
        await cancelled.wait()
    finally:
        _current_request_ctx.reset(token)
        await manager.cancel_background_tasks()

    assert job is not None
    assert job.status == "cancelled"
    assert job.asyncio_task.done()
    assert manager.get_owned_tasks() == set()


# ── 插话队列消费纯函数 ────────────────────────────────────────


def test_drain_interjections_converts_and_clears():
    from agent_core.sub_agent import _drain_interjections

    q = ["重点讲内存安全", "顺便提一下性能"]
    msgs = _drain_interjections(q)
    assert len(msgs) == 2
    assert all(m["role"] == "user" for m in msgs)
    assert "【用户插话】重点讲内存安全" in msgs[0]["content"]
    assert q == [] and _drain_interjections(None) == []


# ── 工具 schema 契约 ─────────────────────────────────────────


def test_control_tool_schema_registered():
    from tool_engine.tool_registry import get_tool

    tool = get_tool("sub_agent_control")
    if tool is None:
        pytest.skip("sub_agent_control 未注册（bootstrap 未初始化的轻量环境）")
    props = tool["schema"]["properties"]
    assert set(props["action"]["enum"]) == {"status", "abort", "interject"}
