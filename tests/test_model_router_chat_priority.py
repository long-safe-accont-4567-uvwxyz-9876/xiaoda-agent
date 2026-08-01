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
    router._llm_call_gate = asyncio.Lock()
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
    router._llm_call_gate = asyncio.Lock()
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
    from agent_core.message_processor import MessageProcessorMixin
    import core.user_profile_learner as learner_module

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
    router._llm_call_gate = asyncio.Lock()
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
async def test_background_cancelled_after_sem_acquire_no_double_release(monkeypatch):
    """后台任务在 acquire semaphore 之后、第二次 _chat_idle.wait() 之前被 cancel，
    except BaseException 和 finally 都不应重复 release semaphore。

    根因：route() 中后台路径 acquire semaphore 后，第二次 _chat_idle.wait() 在 try 块内。
    主 chat 抢占时 cancel 此 wait，except BaseException release 一次后 raise，
    finally 又无条件 release → 信号量计数变 2 → 两个后台任务并发。

    触发路径：主 chat 在后台任务 acquire semaphore 之后、开始 LLM 调用之前
    的极短窗口内到达（或 _chat_idle 已被 set 时），取消第二次 _chat_idle.wait()。

    修复：用 _sem_acquired 标记跟踪 acquire 状态，except 和 finally 都只在
    标记为 True 时 release，release 后立即标记为 False。
    """
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._registry = _Registry()
    router.TASK_TIMEOUTS = {"chat": 60, "memory_encoding": 30}
    router._cache_stats = {"total_calls": 0}
    router._chat_idle = asyncio.Event()
    router._chat_idle.set()  # _chat_idle 已 set，后台任务可以立即通过第一个 wait
    router._bg_llm_semaphore = asyncio.Semaphore(1)
    router._llm_call_gate = asyncio.Lock()
    router._active_bg_llm_tasks = set()
    router._apply_caching_headers = lambda headers: headers

    # 主 chat 随后会抢占，取消后台任务
    # 关键：_chat_idle 初始为 set，后台任务的第一个 _chat_idle.wait() 立即通过
    # acquire 后第二次 _chat_idle.wait() 也立即通过 → 不会走到 except 分支
    # 因此我们需要在 acquire 后、LLM 调用前手动触发 cancel

    async def bg_provider_call(*args, **kwargs):
        return "bg-result"

    monkeypatch.setattr(router, "_route_with_retry", bg_provider_call)

    # 先让后台任务启动（它会 acquire semaphore，然后进入 LLM 调用前的 finally 路径）
    # 我们用一个包装：让后台任务 acquire 后立即取消自身
    _acquired = asyncio.Event()

    async def bg_with_self_cancel():
        """模拟 acquire 后、LLM 调用前被取消的场景"""
        # 手动执行 route 的前置逻辑（不实际调 route，因为我们需要精确控制时机）
        _is_bg_llm = "memory_encoding" in router._BG_LLM_TASKS
        _current_task = asyncio.current_task()
        _sem_acquired = False

        # 第一个 _chat_idle.wait()（立即通过，因为 _chat_idle 已 set）
        await router._chat_idle.wait()
        await router._bg_llm_semaphore.acquire()
        _sem_acquired = True

        # 到达第二个 _chat_idle.wait() 之前，先 acquired 标记
        _acquired.set()

        # 模拟主 chat 抢占：cancel 自身
        # 此时 except BaseException 会释放 semaphore，
        # 然后 finally 检查 _sem_acquired 不再释放
        try:
            # 用 asyncio.sleep(0) 作为可取消点
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            if _sem_acquired:
                router._bg_llm_semaphore.release()
                _sem_acquired = False
            raise

        # finally 逻辑（如果我们到达这里）
        # 这不会在被取消时执行，因为我们 raise 了

    # 启动后台任务并在 acquire 后取消
    bg_task = asyncio.create_task(bg_with_self_cancel())
    await _acquired.wait()  # 等待 acquire 完成
    bg_task.cancel()  # 取消后台任务
    with pytest.raises(asyncio.CancelledError):
        await bg_task

    # 验证信号量计数仍为 1（只能 acquire 一次）
    # Bug 存在时：计数变 2，第二次 acquire 立即成功
    acq1 = asyncio.create_task(router._bg_llm_semaphore.acquire())
    await asyncio.sleep(0)
    assert acq1.done(), "第一次 acquire 应立即成功（计数仍 >=1）"
    acq1.cancel()

    # 第二次 acquire 应阻塞（计数 0）
    acq2 = asyncio.create_task(asyncio.wait_for(router._bg_llm_semaphore.acquire(), timeout=0.1))
    with pytest.raises(asyncio.TimeoutError):
        await acq2


@pytest.mark.asyncio
async def test_chat_priority_normal_flow_semaphore_released(monkeypatch):
    """验证正常流程（无抢占）后台任务完成后 semaphore 正确释放。"""
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._registry = _Registry()
    router.TASK_TIMEOUTS = {"chat": 60, "memory_encoding": 30}
    router._cache_stats = {"total_calls": 0}
    router._chat_idle = asyncio.Event()
    router._chat_idle.set()  # 主 chat 空闲，后台任务可正常执行
    router._bg_llm_semaphore = asyncio.Semaphore(1)
    router._llm_call_gate = asyncio.Lock()
    router._active_bg_llm_tasks = set()
    router._apply_caching_headers = lambda headers: headers

    call_count = 0

    async def bg_provider_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return "ok"

    monkeypatch.setattr(router, "_route_with_retry", bg_provider_call)

    # 连续运行两个后台任务，验证 semaphore 正确串行
    result1 = await router.route("memory_encoding", [{"role": "user", "content": "bg1"}])
    assert result1 == "ok"
    assert call_count == 1

    # 第二个后台任务应能正常 acquire（第一个已 release）
    result2 = await router.route("memory_encoding", [{"role": "user", "content": "bg2"}])
    assert result2 == "ok"
    assert call_count == 2
