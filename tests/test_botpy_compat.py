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