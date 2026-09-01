# tests/test_terminal_flush_backoff.py — 终端输出合帧洪峰自适应退避
"""背景：16ms 固定合帧在弱机/WebView2 上面对超大持续输出(git log、构建
日志等)仍会以 ~60 帧/s 冲击前端渲染。新增按合帧窗字符量指数拉大间隔的
自适应退避（上限 0.25s，回落逐级复原），独立配置项 TERMINAL_FLUSH_ADAPTIVE
默认开启，置 0 关回固定 16ms。本文件覆盖纯函数判定 + 与 _queue_term_output
实际接线的状态迁移。"""
from __future__ import annotations

import asyncio

import pytest

import web.ws_hub as hub


@pytest.fixture
def clean_buffers():
    hub._term_out_buf.clear()
    hub._term_flush_interval.clear()
    yield
    for entry in hub._term_out_buf.values():
        if entry.get("timer") is not None:
            entry["timer"].cancel()
    hub._term_out_buf.clear()
    hub._term_flush_interval.clear()


@pytest.fixture
def fake_session():
    """捕获 send_to 的帧；会话注册放测试体内（需要运行中的事件循环）。"""
    sent: list[dict] = []
    orig_send = hub.manager.send_to

    async def _capture(conn_id, event):
        sent.append(event)

    hub.manager.send_to = _capture
    yield sent
    with hub._pty_sessions_lock:
        hub._pty_sessions.pop("t1", None)
    hub.manager.send_to = orig_send


def _register_fake_session():
    loop = asyncio.get_running_loop()
    with hub._pty_sessions_lock:
        hub._pty_sessions["t1"] = {
            "pid": 0, "fd": -1, "conn_id": "c1", "shell": "bash",
            "alive": True, "loop": loop, "is_windows": False,
        }


# ── 纯函数：间隔判定 ──


def test_flood_window_doubles_until_cap():
    """洪峰窗（≥64KiB）逐级翻倍，封顶 0.25s。"""
    cur = hub._TERM_FLUSH_INTERVAL_S
    steps = []
    for _ in range(5):
        cur = hub._next_flush_interval(cur, hub._TERM_BACKOFF_HIGH_CHARS)
        steps.append(cur)
    assert steps == [0.032, 0.064, 0.128, hub._TERM_FLUSH_MAX_INTERVAL_S,
                     hub._TERM_FLUSH_MAX_INTERVAL_S]


def test_idle_window_halves_until_floor():
    """回落窗（≤2KiB）逐级减半，底限 16ms 固定。"""
    cur = hub._TERM_FLUSH_MAX_INTERVAL_S
    steps = []
    for _ in range(8):
        cur = hub._next_flush_interval(cur, hub._TERM_BACKOFF_LOW_CHARS)
        steps.append(cur)
    assert steps == [0.125, 0.0625, 0.03125, 0.016,
                     0.016, 0.016, 0.016, 0.016]


def test_medium_window_keeps_interval():
    """中窗（2KiB < N < 64KiB）不涨不落。"""
    assert hub._next_flush_interval(0.032, 8 * 1024) == 0.032
    assert hub._next_flush_interval(0.128, 16 * 1024) == 0.128


def test_disabled_flag_always_fixed(monkeypatch):
    """TERMINAL_FLUSH_ADAPTIVE=0：无论窗口多字节都回固定 16ms（旧行为）。

    注意：开关是模块级拷贝，必须打在 web.ws_terminal 本体而非 hub 门面。"""
    import web.ws_terminal as wt
    monkeypatch.setattr(wt, "_TERM_ADAPTIVE_ENABLED", False)
    assert hub._next_flush_interval(
        hub._TERM_FLUSH_MAX_INTERVAL_S, 1024 * 1024) == hub._TERM_FLUSH_INTERVAL_S
    assert hub._next_flush_interval(0.0, 0) == hub._TERM_FLUSH_INTERVAL_S


# ── 集成：与 _queue_term_output 实际接线 ──


@pytest.mark.asyncio
async def test_flood_updates_interval_state(clean_buffers, fake_session):
    """满窗洪峰立即冲刷 → 会话间隔翻倍（真实接线）。"""
    _register_fake_session()
    hub._queue_term_output("t1", "c1", "y" * 72000)  # > MAX_CHARS → 立即冲刷
    await asyncio.sleep(0.02)
    assert hub._term_flush_interval["t1"] == 0.032


@pytest.mark.asyncio
async def test_idle_after_flood_recovers_interval(clean_buffers, fake_session):
    """洪峰后回落到小窗 → 间隔逐级复原到固定 16ms。"""
    _register_fake_session()
    hub._term_flush_interval["t1"] = 0.128  # 模拟此前已退避
    hub._queue_term_output("t1", "c1", "quiet output")  # ≤LOW → 减半
    await asyncio.sleep(0.2)  # 越过 0.128s 合帧窗等冲刷发生
    assert hub._term_flush_interval["t1"] == 0.064


@pytest.mark.asyncio
async def test_cleanup_releases_backoff_state(clean_buffers, fake_session):
    """会话清理必须同步回收退避状态，避免长期驻留。

    假会话注册为 Windows 管道形态（proc=None），cleanup 不触发
    Unix 收割（pid=0 会 SIGKILL 整个进程组，测试进程自杀）。"""
    loop = asyncio.get_running_loop()
    with hub._pty_sessions_lock:
        hub._pty_sessions["t1"] = {
            "pid": 0, "proc": None, "conn_id": "c1", "shell": "cmd",
            "alive": True, "loop": loop, "is_windows": True,
        }
    hub._term_flush_interval["t1"] = 0.128
    hub._queue_term_output("t1", "c1", "x")
    hub._cleanup_pty("t1")
    await asyncio.sleep(0.05)
    assert "t1" not in hub._term_flush_interval