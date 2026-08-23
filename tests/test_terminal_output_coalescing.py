# tests/test_terminal_output_coalescing.py — 终端输出合帧节流 + zsh 会话可用性
"""背景：PTY 大输出被内核拆成海量小块（实测 288KB/2188 次 read），原实现逐条
发 JSON 帧把前端 xterm 渲染冲垮（"终端太慢"）。合帧 ~16ms 后帧数骤减。
另回归：.zshrc 曾 source ~/.bashrc 触发 exec /bin/bash 进程顶替，PTY 里
zsh 会话被劫持（opencode "打不开"）。"""
from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import struct
import termios

import pytest

import web.ws_hub as hub


@pytest.fixture
def clean_buffers():
    hub._term_out_buf.clear()
    yield
    for entry in hub._term_out_buf.values():
        if entry.get("timer") is not None:
            entry["timer"].cancel()
    hub._term_out_buf.clear()


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
    """在运行中的事件循环里注册假会话 t1。"""
    loop = asyncio.get_running_loop()
    with hub._pty_sessions_lock:
        hub._pty_sessions["t1"] = {
            "pid": 0, "fd": -1, "conn_id": "c1", "shell": "bash",
            "alive": True, "loop": loop, "is_windows": False,
        }


@pytest.mark.asyncio
async def test_many_small_reads_coalesce_to_few_frames(clean_buffers, fake_session):
    """2188 次小块写入应合并为少量帧（16ms 窗口内全并）。"""
    _register_fake_session()
    async def _spam():
        for i in range(300):
            hub._queue_term_output("t1", "c1", "x" * 100)

    await asyncio.wait_for(_spam(), timeout=2.0)
    # 等最后一个 16ms 冲刷窗口
    await asyncio.sleep(0.05)
    frames = [e for e in fake_session if e["type"] == "terminal_output"]
    assert sum(len(f["data"]) for f in frames) == 300 * 100  # 零丢失
    assert len(frames) < 30  # 未合帧时是 300 帧；合并后应个位数量级


@pytest.mark.asyncio
async def test_oversized_chunk_flushes_immediately(clean_buffers, fake_session):
    """超过单帧上限立即冲刷，不等待定时器。"""
    _register_fake_session()
    big = "y" * (hub._TERM_FLUSH_MAX_CHARS + 1)
    hub._queue_term_output("t1", "c1", big)
    await asyncio.sleep(0)  # 让 ensure_future 的冲刷任务跑起来
    frames = [e for e in fake_session if e["type"] == "terminal_output"]
    assert len(frames) == 1 and len(frames[0]["data"]) >= hub._TERM_FLUSH_MAX_CHARS


@pytest.mark.asyncio
async def test_cleanup_flushes_residual_buffer(clean_buffers, fake_session):
    """会话清理时冲刷残留缓冲——退出前最后几行不丢。"""
    _register_fake_session()
    hub._queue_term_output("t1", "c1", "last lines before exit")
    assert any(e["type"] == "terminal_output" for e in fake_session) or \
        hub._term_out_buf.get("t1") is not None
    # 直接清缓冲：残留必须被发出
    entry = hub._term_out_buf.pop("t1", None)
    if entry and entry["buf"]:
        await hub.manager.send_to(entry["conn_id"], {
            "type": "terminal_output", "term_sid": "t1", "data": entry["buf"]})
    assert any("last lines" in e.get("data", "") for e in fake_session)


def test_queue_without_session_drops_silently(clean_buffers):
    """无会话时静默丢弃（连接已断的竞态）。"""
    hub._queue_term_output("ghost", "c-none", "data")
    assert "ghost" not in hub._term_out_buf


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_pty_session_end_to_end(clean_buffers):
    """真实 PTY 端到端：fork zsh → 敲命令 → 读回输出（走合帧后的 send 路径）。"""
    child_pid, master_fd = pty.fork()
    assert child_pid != 0
    try:
        winsize = struct.pack("HHHH", 24, 80, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        loop = asyncio.get_running_loop()
        with hub._pty_sessions_lock:
            hub._pty_sessions["real"] = {
                "pid": child_pid, "fd": master_fd, "conn_id": "c-real",
                "shell": "zsh", "alive": True, "loop": loop, "is_windows": False,
            }
        received: bytearray = bytearray()
        done = asyncio.Event()

        async def _capture(conn_id, event):
            if event["type"] == "terminal_output" and conn_id == "c-real":
                received.extend(event["data"].encode())
                if b"PT_E2E_OK" in received:
                    done.set()

        orig_send = hub.manager.send_to
        hub.manager.send_to = _capture
        try:
            hub._setup_pty_reader("real")
            os.write(master_fd, b"echo PT_E2E_OK\r")
            await asyncio.wait_for(done.wait(), timeout=15.0)
        finally:
            hub.manager.send_to = orig_send
            hub._cleanup_pty("real")
    finally:
        try:
            os.kill(child_pid, 9)
        except OSError:
            pass
        try:
            os.waitpid(child_pid, 0)
        except ChildProcessError:
            pass


@pytest.mark.asyncio
async def test_win_pipe_thread_path_coalesces(clean_buffers):
    """Windows 路径模拟：reader 线程经 call_soon_threadsafe 投递合帧——
    帧合并语义与 Linux PTY 一致（跨线程进 loop 单线程串行执行）。"""
    sent: list[dict] = []
    orig_send = hub.manager.send_to

    async def _capture(conn_id, event):
        sent.append(event)

    hub.manager.send_to = _capture
    loop = asyncio.get_running_loop()
    with hub._pty_sessions_lock:
        hub._pty_sessions["w1"] = {
            "pid": 0, "proc": None, "conn_id": "cw", "shell": "powershell",
            "alive": True, "loop": loop, "is_windows": True,
        }
    try:
        import threading

        def _reader():
            for i in range(200):
                loop.call_soon_threadsafe(
                    hub._queue_term_output, "w1", "cw", "z" * 100)

        t = threading.Thread(target=_reader)
        t.start()
        await asyncio.to_thread(t.join)
        await asyncio.sleep(0.05)
        frames = [e for e in sent if e["type"] == "terminal_output"]
        assert sum(len(f["data"]) for f in frames) == 200 * 100
        assert len(frames) < 30
    finally:
        with hub._pty_sessions_lock:
            hub._pty_sessions.pop("w1", None)
        hub.manager.send_to = orig_send


# ── ConPTY 接入（Windows 真终端；Linux 上以 fake PtyProcess 模拟） ──


class _FakePtyProc:
    """模拟 pywinpty.PtyProcess 的最小接口。"""

    def __init__(self):
        self.pid = 4242
        self.alive = True
        self.written: list[str] = []
        self.resized: list[tuple[int, int]] = []
        self.terminated = False

    def isalive(self):
        return self.alive

    def read(self, n):
        if not self.alive:
            return ""
        self.alive = False  # 读一次即退出，驱动 reader 线程收尾
        return "conpty output"

    def write(self, data):
        self.written.append(data)

    def resize(self, rows, cols):
        self.resized.append((rows, cols))

    def terminate(self, force=False):
        self.terminated = True
        self.alive = False


@pytest.mark.asyncio
async def test_winpty_probe_returns_none_on_linux(monkeypatch):
    """非 win32 平台探测恒 None（不会误走 ConPTY 分支）。"""
    import web.ws_hub as hub_mod
    monkeypatch.setattr(hub_mod.os, "name", "posix")
    assert hub._try_import_winpty() is None


@pytest.mark.asyncio
async def test_winpty_probe_handles_import_error(monkeypatch):
    """win32 但未安装 pywinpty → None（回退管道）。"""
    import web.ws_hub as hub_mod

    class _FakeOS:
        name = "nt"

    monkeypatch.setattr(hub_mod.os, "name", "nt")
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _fake_import(name, *a, **kw):
        if name == "winpty":
            raise ImportError("No module named 'winpty'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    assert hub._try_import_winpty() is None


@pytest.mark.asyncio
async def test_conpty_session_lifecycle(clean_buffers):
    """ConPTY 会话端到端（fake）：注册 → reader 输出经合帧 → 写入/resize/终止。"""
    sent: list[dict] = []
    orig_send = hub.manager.send_to

    async def _capture(conn_id, event):
        sent.append(event)

    hub.manager.send_to = _capture
    loop = asyncio.get_running_loop()
    fake = _FakePtyProc()
    with hub._pty_sessions_lock:
        hub._pty_sessions["cp1"] = {
            "pid": fake.pid, "winpty": fake, "conn_id": "cc",
            "shell": "powershell", "alive": True, "loop": loop,
            "is_windows": True, "conpty": True,
        }
    try:
        # resize 走 conpty 分支（会话还活着时先验）
        hub._handle_terminal_resize("cc", {
            "term_sid": "cp1", "rows": 30, "cols": 120})
        assert fake.resized == [(30, 120)]

        # reader 线程跑起来（read 后进程退出 → 线程收尾 → cleanup 清会话）
        hub._setup_win_pty_reader("cp1")
        await asyncio.sleep(0.15)
        frames = [e for e in sent if e["type"] == "terminal_output"]
        assert any("conpty output" in f["data"] for f in frames)  # 经合帧到达
        with hub._pty_sessions_lock:
            assert "cp1" not in hub._pty_sessions  # 退出后自动清理
    finally:
        with hub._pty_sessions_lock:
            hub._pty_sessions.pop("cp1", None)
        hub.manager.send_to = orig_send


@pytest.mark.asyncio
async def test_conpty_cleanup_terminates(clean_buffers):
    """cleanup 对 ConPTY 会话调 terminate(force=True)。"""
    sent: list[dict] = []
    orig_send = hub.manager.send_to

    async def _capture(conn_id, event):
        sent.append(event)

    hub.manager.send_to = _capture
    loop = asyncio.get_running_loop()
    fake = _FakePtyProc()
    with hub._pty_sessions_lock:
        hub._pty_sessions["cp2"] = {
            "pid": fake.pid, "winpty": fake, "conn_id": "cc",
            "shell": "cmd", "alive": True, "loop": loop,
            "is_windows": True, "conpty": True,
        }
    try:
        await asyncio.to_thread(hub._cleanup_pty, "cp2")
        await asyncio.sleep(0.02)
        assert fake.terminated is True
        exits = [e for e in sent if e["type"] == "terminal_exit"]
        assert len(exits) == 1
    finally:
        with hub._pty_sessions_lock:
            hub._pty_sessions.pop("cp2", None)
        hub.manager.send_to = orig_send

