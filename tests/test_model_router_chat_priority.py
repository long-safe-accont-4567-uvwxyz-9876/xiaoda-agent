import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class _Registry:
    def get_task_ref(self, task_type):
        return {"max_tokens": 32, "model": "agnes-2.0-flash"}


@pytest.mark.asyncio
async def test_chat_and_background_llm_never_enter_provider_concurrently(monkeypatch):
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._registry = _Registry()
    router.TASK_TIMEOUTS = {"chat": 60, "memory_encoding": 30}
    router._cache_stats = {"total_calls": 0}
    router._chat_idle = asyncio.Event()
    router._chat_idle.set()
    router._bg_llm_semaphore = asyncio.Semaphore(1)
    router._active_bg_llm_tasks = set()
    router._apply_caching_headers = lambda headers: headers
    active = 0
    overlap = False
    entered = asyncio.Event()
    release = asyncio.Event()

    async def provider_call(*args, **kwargs):
        nonlocal active, overlap
        active += 1
        overlap = overlap or active > 1
        entered.set()
        try:
            await release.wait()
            return "ok"
        finally:
            active -= 1

    monkeypatch.setattr(router, "_route_with_retry", provider_call)

    background = asyncio.create_task(
        router.route("memory_encoding", [{"role": "user", "content": "bg"}])
    )
    await entered.wait()
    chat = asyncio.create_task(
        router.route("chat", [{"role": "user", "content": "chat"}])
    )
    await asyncio.sleep(0)
    assert active == 1
    assert not chat.done()
    release.set()
    await chat
    with pytest.raises(asyncio.CancelledError):
        await background
    assert not overlap


@pytest.mark.asyncio
async def test_chat_cancels_active_background_llm_and_enters_provider(monkeypatch):
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._registry = _Registry()
    router.TASK_TIMEOUTS = {"chat": 60, "memory_encoding": 30}
    router._cache_stats = {"total_calls": 0}
    router._chat_idle = asyncio.Event()
    router._chat_idle.set()
    router._bg_llm_semaphore = asyncio.Semaphore(1)
    router._active_bg_llm_tasks = set()
    router._apply_caching_headers = lambda headers: headers
    background_started = asyncio.Event()
    chat_started = asyncio.Event()

    async def provider_call(task_type, *args, **kwargs):
        if task_type == "memory_encoding":
            background_started.set()
            await asyncio.Event().wait()
        chat_started.set()
        return "ok"

    monkeypatch.setattr(router, "_route_with_retry", provider_call)
    background = asyncio.create_task(
        router.route("memory_encoding", [{"role": "user", "content": "bg"}])
    )
    await background_started.wait()
    chat = asyncio.create_task(
        router.route("chat", [{"role": "user", "content": "chat"}])
    )
    await asyncio.wait_for(chat_started.wait(), timeout=0.2)
    assert background.cancelled()
    assert await chat == "ok"


@pytest.mark.asyncio
async def test_profile_insight_uses_background_llm_route(monkeypatch):
    import core.user_profile_learner as learner_module
    from agent_core.message_processor import MessageProcessorMixin

    learner = MagicMock()
    learner.build_insight_prompt.return_value = "extract profile"
    monkeypatch.setattr(learner_module, "get_user_profile_learner", lambda: learner)
    processor = SimpleNamespace(
        context=SimpleNamespace(get_last_n=lambda count: [{"role": "user", "content": "hi"}]),
        router=SimpleNamespace(route=AsyncMock(return_value="insight")),
    )

    await MessageProcessorMixin._run_profile_insight(processor, "user-1", 3)

    assert processor.router.route.await_args.kwargs["task_type"] == "memory_encoding"


@pytest.mark.asyncio
async def test_background_cancelled_before_sem_acquire_does_not_leak_semaphore(monkeypatch):
    """后台任务在 acquire semaphore 之前被 cancel，不得 release 未 acquire 的 semaphore。

    根因：route() 的 finally 块无条件 release _bg_llm_semaphore。但若 cancel 发生在
    ``await self._chat_idle.wait()``（acquire 之前，且该语句在 try 块外），semaphore
    从未 acquire，release 会导致计数 > 初始值，破坏后台串行保证 → 允许并发竞争
    agnes API → 主 chat 阻塞。

    触发路径：instinct_manager/nudge_engine/reunion_reflection/growth_narrative/
    entity_extractor/knowledge_graph 等均用 ``asyncio.wait_for(route("memory_encoding"),
    timeout)`` 包裹后台 LLM 调用。主 chat 占用 _chat_idle 时，后台任务卡在
    _chat_idle.wait()，wait_for 超时 cancel route 协程 → finally release 未 acquire
    的 semaphore → Semaphore(1) 计数变 2 → 后台串行保证失效。

    修复：用 _sem_acquired 标志位跟踪是否 acquire，finally 只在 acquire 过时才 release。
    """
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._registry = _Registry()
    router.TASK_TIMEOUTS = {"chat": 60, "memory_encoding": 30}
    router._cache_stats = {"total_calls": 0}
    router._chat_idle = asyncio.Event()
    router._chat_idle.set()
    router._bg_llm_semaphore = asyncio.Semaphore(1)
    router._active_bg_llm_tasks = set()
    router._apply_caching_headers = lambda headers: headers

    async def provider_call(*args, **kwargs):
        return "should-not-reach"  # 后台应卡在 _chat_idle.wait()，不该到 provider

    monkeypatch.setattr(router, "_route_with_retry", provider_call)

    # 主 chat 占用 _chat_idle（模拟主 chat 正在执行）
    router._chat_idle.clear()

    # 后台任务卡在 _chat_idle.wait()（acquire semaphore 之前）
    # 用 wait_for 包裹，模拟 instinct_manager 的 timeout=10s 超时 cancel
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            router.route("memory_encoding", [{"role": "user", "content": "bg"}]),
            timeout=0.1,
        )

    # 修复后：semaphore 计数仍为 1，只能 acquire 一次
    # Bug 存在时：semaphore 计数变 2，能 acquire 两次
    acq1 = asyncio.create_task(router._bg_llm_semaphore.acquire())
    await asyncio.sleep(0)
    assert acq1.done() and not acq1.cancelled(), "第一次 acquire 应立即成功（计数仍 >=1）"

    # 第二次 acquire 应阻塞（计数 0）—— 用 wait_for 验证会超时
    acq2 = asyncio.create_task(asyncio.wait_for(router._bg_llm_semaphore.acquire(), timeout=0.1))
    with pytest.raises(asyncio.TimeoutError):
        await acq2
    acq1.cancel()  # 清理


@pytest.mark.asyncio
async def test_chat_cancel_in_preempt_window_restores_idle_and_unblocks_bg(monkeypatch):
    """chat 抢占窗口内被取消，_chat_idle 必须恢复 set，后台任务不得死锁。

    根因：route() 的 chat 分支 ``_chat_idle.clear()`` 原先位于任何 try/finally 之外，
    其后有 ``await asyncio.sleep(0)``。调用方（main_path.py）用
    ``asyncio.wait_for(route("chat"), LLM_CALL_TIMEOUT)`` 包裹；超时取消若恰好落在
    clear 之后、主 try 之前，finally 的 ``set()`` 不会执行，_chat_idle 永久保持
    cleared → memory_encoding/emotion_analysis 等后台 LLM 任务全部死锁在
    ``_chat_idle.wait()``，静默饥饿。

    触发方式：用可门控的假 sleep 替换全局 asyncio.sleep，把取消精确注入
    clear 之后的 await 窗口内。
    """
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._registry = _Registry()
    router.TASK_TIMEOUTS = {"chat": 60, "memory_encoding": 30}
    router._cache_stats = {"total_calls": 0}
    router._chat_idle = asyncio.Event()
    router._chat_idle.set()
    router._bg_llm_semaphore = asyncio.Semaphore(1)
    router._active_bg_llm_tasks = set()
    router._apply_caching_headers = lambda headers: headers

    entered_window = asyncio.Event()
    proceed = asyncio.Event()

    async def gated_sleep(*args, **kwargs):
        # 走到这里说明 clear() 已执行、尚未进入主 try —— 正是取消安全缺口窗口
        entered_window.set()
        await proceed.wait()

    monkeypatch.setattr(asyncio, "sleep", gated_sleep)

    async def provider_call(*args, **kwargs):
        return "bg-ok"

    monkeypatch.setattr(router, "_route_with_retry", provider_call)

    chat = asyncio.create_task(
        router.route("chat", [{"role": "user", "content": "hi"}])
    )
    await entered_window.wait()  # 已过 clear()，停在窗口内
    chat.cancel()
    proceed.set()  # 唤醒后 CancelledError 在窗口内的 await 点抛出
    with pytest.raises(asyncio.CancelledError):
        await chat
    assert chat.cancelled()

    # (a) 取消后 _chat_idle 必须恢复 set（bug 存在时永久保持 cleared）
    assert router._chat_idle.is_set(), \
        "_chat_idle 在抢占窗口内被取消后必须恢复 set，否则后台任务死锁"

    # (b) 后台任务能继续获得信号量并执行完毕（死锁存在时这里 TimeoutError）
    bg_result = await asyncio.wait_for(
        router.route("memory_encoding", [{"role": "user", "content": "bg"}]),
        timeout=5,
    )
    assert bg_result == "bg-ok"
    assert not router._bg_llm_semaphore.locked(), "后台完成后 semaphore 必须释放"
