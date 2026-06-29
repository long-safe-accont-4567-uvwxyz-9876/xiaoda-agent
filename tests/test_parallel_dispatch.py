"""测试 SubAgentManagerMixin.parallel_dispatch — 无依赖任务并行调度。

覆盖：空目标 / 单目标串行路径 / 多目标并行 / 单任务异常不阻塞 / 顺序保持 /
并行快于串行。使用 pytest + pytest-asyncio + AsyncMock。
"""
import asyncio
import time

import pytest
from unittest.mock import AsyncMock

from agent_core._shared import ProcessResult, RequestContext
from agent_core.sub_agent_manager import SubAgentManagerMixin


class StubSubAgentManager(SubAgentManagerMixin):
    """最小桩：仅实现 parallel_dispatch 依赖的 _dispatch_single_sub_agent。

    parallel_dispatch 只调用 _dispatch_single_sub_agent，因此不需要 Mixin 其他
    依赖（dispatcher/context/security/...），即可隔离测试并行调度逻辑。
    """

    def __init__(self) -> None:
        self.dispatch_mock = AsyncMock()

    async def _dispatch_single_sub_agent(self, target, clean_input, user_id, source,
                                          session_id, trace, force_voice: bool = False,
                                          ctx: RequestContext | None = None) -> ProcessResult:
        return await self.dispatch_mock(
            target, clean_input, user_id, source, session_id, trace,
            force_voice=force_voice, ctx=ctx,
        )


def _make_result(reply: str, *, error: str = "") -> ProcessResult:
    return ProcessResult(reply=reply, error=error)


class _FakeTrace:
    """模拟 trace 对象，记录 info/error 调用便于断言。"""

    def __init__(self) -> None:
        self.calls: list = []

    def info(self, *a, **k) -> None:
        self.calls.append(("info", a, k))

    def error(self, *a, **k) -> None:
        self.calls.append(("error", a, k))


@pytest.fixture
def manager() -> StubSubAgentManager:
    return StubSubAgentManager()


@pytest.fixture
def trace() -> _FakeTrace:
    return _FakeTrace()


@pytest.mark.asyncio
async def test_empty_targets_returns_empty(manager, trace):
    """空目标列表应直接返回空结果，不调用任何子代理。"""
    results = await manager.parallel_dispatch([], "u1", "qq", "s1", trace)
    assert results == []
    manager.dispatch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_single_target_uses_serial_path(manager, trace):
    """单目标走串行路径：仅调用一次 _dispatch_single_sub_agent。"""
    manager.dispatch_mock.return_value = _make_result("hi from keli")
    results = await manager.parallel_dispatch(
        [("keli", "你好")], "u1", "qq", "s1", trace
    )
    assert len(results) == 1
    assert results[0].reply == "hi from keli"
    assert results[0].error == ""
    manager.dispatch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_targets_parallel(manager, trace):
    """多目标并行执行：每个目标都被调度，结果顺序与输入一致。"""
    async def _fake(target, *args, **kwargs):
        return _make_result(f"reply:{target}")

    manager.dispatch_mock.side_effect = _fake

    targets_inputs = [("keli", "问1"), ("yinlang", "问2"), ("nike", "问3")]
    results = await manager.parallel_dispatch(targets_inputs, "u1", "qq", "s1", trace)

    assert len(results) == 3
    assert [r.reply for r in results] == ["reply:keli", "reply:yinlang", "reply:nike"]
    assert manager.dispatch_mock.await_count == 3


@pytest.mark.asyncio
async def test_exception_in_one_target_returns_error_result(manager, trace):
    """一个任务抛异常不阻塞其他任务，异常归一化为带 error 的 ProcessResult。"""
    async def _fake(target, *args, **kwargs):
        if target == "yinlang":
            raise RuntimeError("boom")
        return _make_result(f"reply:{target}")

    manager.dispatch_mock.side_effect = _fake

    targets_inputs = [("keli", "问1"), ("yinlang", "问2"), ("nike", "问3")]
    results = await manager.parallel_dispatch(targets_inputs, "u1", "qq", "s1", trace)

    assert len(results) == 3
    # 正常任务不受影响
    assert results[0].reply == "reply:keli"
    assert results[0].error == ""
    assert results[2].reply == "reply:nike"
    assert results[2].error == ""
    # 异常任务归一化为错误结果
    assert results[1].error == "boom"
    assert "暂时无法响应" in results[1].reply


@pytest.mark.asyncio
async def test_results_order_matches_input(manager, trace):
    """结果顺序严格与输入顺序一致，即使各任务完成时间不同。"""
    async def _fake(target, *args, **kwargs):
        # 让后输入的目标先完成（sleep 短），验证顺序仍按输入
        delay = {"keli": 0.05, "yinlang": 0.01, "nike": 0.03}.get(target, 0.01)
        await asyncio.sleep(delay)
        return _make_result(f"reply:{target}")

    manager.dispatch_mock.side_effect = _fake

    targets_inputs = [("keli", "x"), ("yinlang", "x"), ("nike", "x")]
    results = await manager.parallel_dispatch(targets_inputs, "u1", "qq", "s1", trace)

    assert [r.reply for r in results] == ["reply:keli", "reply:yinlang", "reply:nike"]


@pytest.mark.asyncio
async def test_parallel_faster_than_serial(manager, trace):
    """并行执行应比串行快：每任务 sleep 0.05s，3 个并行 ≈0.05s，串行 ≈0.15s。"""
    PER_TASK = 0.05

    async def _fake(target, *args, **kwargs):
        await asyncio.sleep(PER_TASK)
        return _make_result(f"reply:{target}")

    manager.dispatch_mock.side_effect = _fake

    targets_inputs = [("keli", "x"), ("yinlang", "x"), ("nike", "x")]

    # 并行调度
    t0 = time.monotonic()
    await manager.parallel_dispatch(targets_inputs, "u1", "qq", "s1", trace)
    parallel_elapsed = time.monotonic() - t0

    # 串行逐个调度（对照基线）
    t1 = time.monotonic()
    for target, inp in targets_inputs:
        await manager._dispatch_single_sub_agent(target, inp, "u1", "qq", "s1", trace)
    serial_elapsed = time.monotonic() - t1

    assert parallel_elapsed < serial_elapsed
    # 并行应明显快于串行（至少快 30%）
    assert parallel_elapsed < serial_elapsed * 0.7
