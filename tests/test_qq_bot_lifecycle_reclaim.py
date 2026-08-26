"""QQ NudgeEngine 泄漏回归（后端可靠性小任务 B / T2）。

原缺陷：run_qq_bot 重连循环每次新建 AIQQBot 只在 CancelledError 分支
client.close()；on_ready 启动 nudge_engine（周期 60s 任务），全仓无任何
stop 调用——重连/凭证轮换后旧 bot 的 nudge 周期任务永久泄漏，旧实例全部
不可回收。

修复契约：
1. AIQQBot 提供统一 async close()：幂等 stop nudge_engine（NudgeEngine.stop
   幂等）、关闭 SDK client（botpy Client.close）；
2. run_qq_bot 重连循环每次迭代结束（正常退出 / 异常重连前）都回收当前
   client 实例——凭证轮换重启时旧实例（含 nudge 任务）全部被取消；
3. NudgeEngine.stop() 幂等：重复调用/未 start 时调用均安全。
"""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

import qq_bot_adapter as qba
from emotion.nudge_engine import NudgeEngine

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class _FakeSDKClient:
    """记录 close 次数的 botpy.Client 替身。"""

    def __init__(self):
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class _RecordingNudge(NudgeEngine):
    """记录 stop/start 的 NudgeEngine 替身（真实 _loop 行为保留）。"""

    def __init__(self):
        self.start_count = 0
        self.stop_count = 0
        self._running = False
        self._task = None
        self._user_openid = "openid-test"

    async def start(self) -> None:
        self.start_count += 1
        await super().start()

    async def stop(self) -> None:
        self.stop_count += 1
        await super().stop()


def _make_bot(sdk_close_count: dict | None = None) -> qba.AIQQBot:
    """构造绕过 botpy.Client.__init__ 的 AIQQBot（补齐 close() 依赖的状态）。"""
    bot = qba.AIQQBot.__new__(qba.AIQQBot)
    sdk = _FakeSDKClient()
    # 对齐 botpy.Client：close() 读取的私有状态由 __init__ 建立
    object.__setattr__(bot, "_closed", False)
    object.__setattr__(bot, "http", SimpleNamespace(close=sdk.close))
    if sdk_close_count is not None:
        sdk_close_count["client"] = sdk
    return bot


# ---------------------------------------------------------------------------
# 1. NudgeEngine.stop 幂等
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nudge_engine_stop_is_idempotent():
    engine = NudgeEngine(db=SimpleNamespace(), analytics=SimpleNamespace(),
                         router=SimpleNamespace(), api=SimpleNamespace(),
                         user_openid="openid-x")
    # 未 start 直接 stop：安全
    await engine.stop()
    await engine.start()
    task = engine._task
    assert task is not None and not task.done()
    await engine.stop()
    await engine.stop()  # 重复 stop 不抛错
    for _ in range(50):
        await asyncio.sleep(0.01)
        if task.done():
            break
    assert task.done(), "stop 后周期任务应被取消"


# ---------------------------------------------------------------------------
# 2. adapter.close() 统一回收 nudge engine + SDK client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adapter_close_stops_nudge_and_closes_client():
    tracked: dict = {}
    bot = _make_bot(tracked)
    nudge = _RecordingNudge()
    await nudge.start()
    nudge_task = nudge._task
    bot.nudge_engine = nudge

    await bot.close()

    assert nudge.stop_count == 1, "close 应停止 nudge engine"
    for _ in range(100):
        await asyncio.sleep(0.01)
        if nudge_task.done():
            break
    assert nudge_task.done(), "nudge 周期任务应被取消"
    assert tracked["client"].close_count == 1, "close 应关闭 SDK client"
    # 幂等：重复 close 不抛错、不二次计数、不重复关闭 client
    await bot.close()
    assert nudge.stop_count == 1, "adapter close 幂等：不应二次触发 nudge stop"
    assert tracked["client"].close_count == 1, "已关闭的 client 不应重复关闭"

    # 无 nudge engine 时 close 也安全
    bot2 = _make_bot()
    bot2.nudge_engine = None
    await bot2.close()


@pytest.mark.asyncio
async def test_adapter_close_tolerates_nudge_stop_failure():
    bot = _make_bot()

    class _BrokenNudge:
        async def stop(self):
            raise RuntimeError("nudge stop exploded")

    bot.nudge_engine = _BrokenNudge()
    await bot.close()  # 异常被吞掉，不阻断 client 关闭


# ---------------------------------------------------------------------------
# 3. run_qq_bot 重连后旧实例被回收（nudge 任务取消 + client 关闭）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_qq_bot_reclaims_old_instance_on_reconnect(monkeypatch):
    """重连两次：每个旧 client 都被 close、其 nudge 周期任务都被取消。"""
    created: list[SimpleNamespace] = []
    closed_clients: list[_FakeSDKClient] = []
    nudges: list[_RecordingNudge] = []

    class _ReconnectThenDieClient(SimpleNamespace):
        """start() 立即返回（模拟 ws 断开），触发外层 while 重建实例。"""

        def __init__(self, intents=None, is_sandbox=False, timeout=30, agent=None):
            super().__init__(
                intents=intents, is_sandbox=is_sandbox, timeout=timeout, agent=agent,
                http=None, _closed=False,
                close_count=0,
            )
            created.append(self)

        async def start(self, appid="", secret=""):
            await asyncio.sleep(0)  # 立即断开
            return None

        async def close(self):
            if not self._closed:
                self._closed = True
                self.close_count += 1
                closed_clients.append(self)

    class _AutoStartNudge(_RecordingNudge):
        pass

    monkeypatch.setattr(qba, "AIQQBot", _ReconnectThenDieClient)

    real_sleep = asyncio.sleep

    async def _fast_backoff(delay):
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(qba.asyncio, "sleep", _fast_backoff)
    monkeypatch.setattr(qba.os, "getenv",
                        lambda k, d="": {"QQBOT_APP_ID": "app", "QQBOT_APP_SECRET": "sec"}.get(k, d))
    monkeypatch.setattr(qba, "redirect_bot_log", lambda: None)

    agent = SimpleNamespace(nudge_engine=None)

    async def _on_ready_replacement(self):
        # 模拟 on_ready 启动 nudge engine（真实路径）
        if self.nudge_engine is None:
            nudge = _RecordingNudge()
            await nudge.start()
            nudges.append(nudge)
            self.nudge_engine = nudge

    monkeypatch.setattr(
        qba.AIQQBot, "_original_on_ready", property(lambda self: _on_ready_replacement),
        raising=False,
    )

    task = asyncio.ensure_future(qba.run_qq_bot(agent))
    # 等 3 个实例被创建（初次 + 重连两次）
    for _ in range(400):
        await real_sleep(0.01)
        if len(created) >= 3:
            break
    assert len(created) >= 3, f"应至少创建 3 个实例（初次+重连两次），实际 {len(created)}"

    # 修复契约：每个被替换的旧实例都应被回收——client 关闭、其 nudge 周期任务取消。
    # （旧实现：非 CancelledError 分支只 sleep 重连，从不 close()；on_ready 新建的
    #   nudge engine 随实例一起被丢弃但周期任务永不停止。）
    assert all(c.close_count >= 1 for c in created[:-1]), \
        "重连后每个旧 client 都应被 close 回收"
    assert all(n.stop_count >= 1 for n in nudges[:-1]), \
        "重连后旧实例的 nudge engine 都应被 stop 回收"
    # 当前活跃实例（最后一个）仍在运行：nudge 未停
    if nudges:
        assert nudges[-1].stop_count == 0

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
