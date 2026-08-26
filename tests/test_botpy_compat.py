"""botpy_compat 兼容层测试（原 test_botpy_log_redirect 并入）。

覆盖：
1. 文件日志重定向（目标 LOG_DIR、幂等、失败静默回退）——原 3 项
2. install_botpy_patches 安装与幂等
3. check_sdk_compat 探针（真实 SDK 下应无漂移）
"""
import logging
import logging.handlers
import os
import time
from pathlib import Path

import botpy
import botpy.logging as bl
import pytest

import botpy_compat as bc


def _reset_botpy_state():
    """重置 SDK 日志模块状态，模拟新进程首次配置。"""
    bl._ext_handlers = []
    bl.logs.clear()
    lg = logging.getLogger("botpy")
    for h in list(lg.handlers):
        lg.removeHandler(h)


@pytest.fixture(autouse=True)
def _sdk_clean_state():
    _reset_botpy_state()
    bc.reset_redirect_flag()
    bc.reset_install_flag()
    yield
    _reset_botpy_state()
    bc.reset_redirect_flag()
    bc.reset_install_flag()


def _trigger_construct() -> None:
    """模拟 AIQQBot 实例化（触发 SDK configure_logging 与 handler 挂载，不触网）。

    SDK 某些版本/进程状态下 Client 构造会因已注册的 asyncio loop 抛
    RuntimeError；文件 handler 的挂载与构造行为解耦（configure_logging +
    get_logger），构造失败时直接显式 get_logger() 完成同样的挂载验证。
    """
    try:
        from botpy.client import Client

        Client(intents=botpy.Intents(public_messages=True))
    except (RuntimeError, NotImplementedError) as exc:  # 构造环境不兼容仅告警
        logging.getLogger("botpy").getChild("construct_skip").debug(str(exc)[:120])
        botpy.logging.get_logger()


# ── 日志重定向 ──────────────────────────────────────────────

def test_redirect_target_and_no_cwd_file(tmp_path):
    log_dir = tmp_path / "logs"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    old_cwd = Path.cwd()
    os.chdir(cwd)
    try:
        bc.redirect_bot_log(log_dir=log_dir)
        _trigger_construct()
        logging.getLogger("botpy").warning("probe-after-redirect")
        time.sleep(0.2)  # 等待 TimedRotatingFileHandler 刷盘
        target = log_dir / "botpy.log"
        assert target.exists(), f"{target} 未创建"
        content = target.read_text(encoding="utf-8")
        assert "probe-after-redirect" in content
        assert not (cwd / "botpy.log").exists(), "cwd 不应再产生 botpy.log"
    finally:
        os.chdir(old_cwd)


def test_redirect_idempotent(tmp_path):
    bc.redirect_bot_log(log_dir=tmp_path / "logs")
    bc.redirect_bot_log(log_dir=tmp_path / "logs")
    _trigger_construct()
    lg = logging.getLogger("botpy")
    file_handlers = [
        h for h in lg.handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(file_handlers) == 1, f"文件 handler 应唯一，实际 {len(file_handlers)}"


def test_redirect_failure_does_not_raise(tmp_path):
    """目标路径不可用（落在已存在文件之下）时静默回退，不抛异常。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    bad_log_dir = blocker / "logs"  # mkdir 在文件路径下必然抛 FileNotFoundError
    bc.redirect_bot_log(log_dir=bad_log_dir)  # 不抛即视为回退成功


# ── 补丁安装 ────────────────────────────────────────────────

def test_install_patches_idempotent():
    from botpy.client import Client
    from botpy.gateway import BotWebSocket

    assert not bc._PATCH_INSTALLED
    bc.install_botpy_patches()
    assert bc._PATCH_INSTALLED is True

    is_orig = BotWebSocket._is_system_event
    beat_orig = BotWebSocket._send_heart
    closed_orig = BotWebSocket.on_closed
    pool_orig = Client._pool_init

    bc.install_botpy_patches()  # 第二次调用必须无副作用
    assert BotWebSocket._is_system_event is is_orig
    assert BotWebSocket._send_heart is beat_orig
    assert BotWebSocket.on_closed is closed_orig
    assert Client._pool_init is pool_orig

    # 对应原始方法应被保存
    assert bc._original_is_system_event is not None
    assert bc._original_send_heart is not None
    assert bc._original_on_closed is not None
    assert bc._original_pool_init is not None


def test_compat_probe_on_real_sdk():
    """真实安装的 qq-botpy 上探针应报告无漂移。"""
    drift = bc.check_sdk_compat()
    assert drift == [], f"SDK 适配漂移: {drift}"


# ── loop 异常处理器（ZeroDivisionError 不再停共享 loop）──────

class _FakeLog:
    """替换 botpy.logging 出口，捕获 handler 的处置日志。"""

    def __init__(self):
        self.records: list[str] = []

    def error(self, message, *args):
        self.records.append(message % args if args else message)


def _make_shim_connection(cancelled: list):
    """在名为 botpy.* 的伪模块内定义 ConnectionSession._runner 并实例化。

    同时满足 handler 的两个识别条件：runner 帧 globals.__name__ 以 botpy
    开头、f_locals["self"] is 该实例（真实绑定方法，无需注入帧）。
    返回的实例挂 .loop 属性后即可作为 fake client 的 _connection。
    """

    ns = {
        "__name__": "botpy.test_shim",
        "asyncio": __import__("asyncio"),
        "cancelled": cancelled,
    }
    exec(
        "class ConnectionSession:\n"
        "    async def _runner(self):\n"
        "        try:\n"
        "            await asyncio.sleep(3600)\n"
        "        except asyncio.CancelledError:\n"
        "            cancelled.append(True)\n"
        "            raise\n",
        ns,
    )
    return ns["ConnectionSession"]()


def _install_handler(monkeypatch, log: _FakeLog, conn):
    """安装 handler 到新 loop，返回 (loop, handler)。"""
    import asyncio
    from types import SimpleNamespace

    monkeypatch.setattr(bc, "_log_botpy_error", log.error)
    loop = asyncio.new_event_loop()
    conn.loop = loop
    bc._install_loop_handler(SimpleNamespace(_connection=conn))
    return loop, loop.get_exception_handler()


def test_zde_from_botpy_cancels_runners_not_loop(monkeypatch):
    """botpy 来源的 ZeroDivisionError → 取消本 Client 的 session runner，loop 存活。"""
    import asyncio

    log = _FakeLog()
    cancelled: list = []
    conn = _make_shim_connection(cancelled)
    loop, _handler = _install_handler(monkeypatch, log, conn)
    try:
        task = loop.create_task(conn._runner())

        async def main():
            await asyncio.sleep(0)  # 让 task 起跑进入 sleep
            handler = loop.get_exception_handler()
            assert handler is not None, "handler 未安装"
            handler(loop, {"exception": ZeroDivisionError("boom"), "task": task})
            await asyncio.sleep(0.05)
            return task

        task = loop.run_until_complete(main())
        assert cancelled == [True], "session runner 应被取消以触发重连"
        assert task.cancelled(), "runner 任务应处于已取消态"
        assert not loop.is_closed(), "共享 loop 绝不能被停止"
        assert any("session runner" in r for r in log.records)
    finally:
        loop.close()


def test_zde_from_non_botpy_is_log_only(monkeypatch):
    """非 botpy 来源的 ZeroDivisionError → 仅记录，不取消任何任务。"""
    import asyncio

    log = _FakeLog()
    cancelled: list = []
    conn = _make_shim_connection(cancelled)
    loop, _handler = _install_handler(monkeypatch, log, conn)
    try:
        state = {"cancelled": False}

        async def webui_coro():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async def main():
            task = loop.create_task(webui_coro())
            await asyncio.sleep(0)
            handler = loop.get_exception_handler()
            handler(loop, {"exception": ZeroDivisionError("boom"), "task": task})
            await asyncio.sleep(0.05)
            assert not state["cancelled"], "非 botpy 来源不应取消任务"

        loop.run_until_complete(main())
        assert any("仅记录不停服" in r for r in log.records)
    finally:
        loop.close()


def test_zde_without_task_context_degrades_to_log_only(monkeypatch):
    """context 缺 future/task（如裸 handle 异常）→ 归因不到来源，保守仅记录。"""

    log = _FakeLog()
    cancelled: list = []
    conn = _make_shim_connection(cancelled)
    loop, _handler = _install_handler(monkeypatch, log, conn)
    try:
        async def main():
            handler = loop.get_exception_handler()
            handler(loop, {"exception": ZeroDivisionError("boom")})

        loop.run_until_complete(main())
        assert cancelled == [], "无来源信息时不应误杀会话任务"
        assert any("仅记录不停服" in r for r in log.records)
    finally:
        loop.close()


def test_non_zde_is_untouched(monkeypatch):
    """非 ZeroDivisionError 异常 → 除默认记录外不做任何处置。"""

    log = _FakeLog()
    cancelled: list = []
    conn = _make_shim_connection(cancelled)
    loop, _handler = _install_handler(monkeypatch, log, conn)
    try:
        async def main():
            handler = loop.get_exception_handler()
            handler(loop, {"exception": RuntimeError("x"), "future": None})

        loop.run_until_complete(main())
        assert cancelled == []
        assert log.records == [], "非 ZDE 不应有处置日志"
    finally:
        loop.close()
